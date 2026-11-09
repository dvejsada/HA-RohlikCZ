"""Tests for parsing the delivery ETA out of Rohlík's delivery announcement."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.rohlikcz.utils import extract_delivery_datetime

PRAGUE = ZoneInfo("Europe/Prague")
RECEIVED_AT = datetime(2026, 9, 20, 10, 3, 36, tzinfo=PRAGUE)
HIGHLIGHT = '<span style="color:#009B37">{}</span>'


@pytest.fixture(autouse=True)
def _frozen_clock(freezer) -> None:
    """Run each parse at the moment the announcement is received."""
    freezer.move_to(RECEIVED_AT)


@pytest.mark.parametrize(
    ("content", "minutes"),
    [
        ("Váš nákup doručíme přibližně za 2 minuty.", 2),
        ("Váš nákup doručíme přibližně za 9 minut.", 9),
        ("Váš nákup doručíme přibližně za 1 minutu.", 1),
        ("Váš nákup doručíme přibližně za minutu.", 1),
        (f"Váš nákup doručíme přibližně za {HIGHLIGHT.format('2 minuty')}.", 2),
        (f"Váš nákup doručíme přibližně za {HIGHLIGHT.format(3)} minuty.", 3),
        ("Váš nákup doručíme přibližně za&nbsp;2&nbsp;minuty.", 2),
        ("Váš nákup doručíme přibližně za\u00a02 minuty.", 2),
        ("Váš nákup doručíme přibližně za 2–3 minuty.", 2),
        ("Váš nákup doručíme přibližně\\nza 5 minut.", 5),
        ("Kurýr dorazí za přibližně 5 minut.", 5),
        ("Doručíme za cca 5 minut.", 5),
        ("Doručíme za 2 až 3 minuty.", 2),
        # A minute countdown beats an unhighlighted time mentioned alongside it.
        ("Doručíme za 5 minut (okno 10:00–11:00).", 5),
    ],
)
def test_minutes_until_delivery(content: str, minutes: int) -> None:
    """The short announcement without a clock time yields a relative ETA."""
    assert extract_delivery_datetime(content, RECEIVED_AT) == RECEIVED_AT + timedelta(
        minutes=minutes
    )


@pytest.mark.parametrize(
    "content",
    [
        f"Váš nákup doručíme přibližně za 10 minut, tedy v {HIGHLIGHT.format('10:11')}.",
        f"Váš nákup doručíme přibližně za {HIGHLIGHT.format(10)} minut, "
        f"tedy v {HIGHLIGHT.format('10:11')}.",
    ],
)
def test_clock_time_wins_over_minute_count(content: str) -> None:
    """The long announcement keeps using its exact clock time."""
    assert extract_delivery_datetime(content, RECEIVED_AT) == datetime(
        2026, 9, 20, 10, 11, tzinfo=PRAGUE
    )


@pytest.mark.parametrize(
    ("received_at", "expected"),
    [
        # Received before the ETA.
        (datetime(2026, 9, 20, 10, 0, tzinfo=PRAGUE), datetime(2026, 9, 20, 10, 4, tzinfo=PRAGUE)),
        # Re-read after the ETA passed (late courier, restart): still today.
        (datetime(2026, 9, 20, 10, 50, tzinfo=PRAGUE), datetime(2026, 9, 20, 10, 4, tzinfo=PRAGUE)),
        # Long past: the time refers to tomorrow.
        (datetime(2026, 9, 20, 20, 0, tzinfo=PRAGUE), datetime(2026, 9, 21, 10, 4, tzinfo=PRAGUE)),
    ],
)
def test_clock_time_day_resolution(received_at: datetime, expected: datetime) -> None:
    """A bare clock time is placed on the reference day unless clearly passed."""
    content = f"Doručíme v {HIGHLIGHT.format('10:04')}"

    assert extract_delivery_datetime(content, received_at) == expected


def test_date_and_time() -> None:
    """A date plus time announcement is unaffected."""
    content = f"Doručíme {HIGHLIGHT.format('21.9.')} v {HIGHLIGHT.format('08:00')}"

    assert extract_delivery_datetime(content, RECEIVED_AT) == datetime(
        2026, 9, 21, 8, 0, tzinfo=PRAGUE
    )


def test_clock_time_in_shop_timezone() -> None:
    """Clock times are read in the shop's own timezone (Sezamo.ro: Bucharest)."""
    bucharest = ZoneInfo("Europe/Bucharest")
    content = f"Livrăm la {HIGHLIGHT.format('12:30')}"

    assert extract_delivery_datetime(content, RECEIVED_AT, tz=bucharest) == datetime(
        2026, 9, 20, 12, 30, tzinfo=bucharest
    )


def test_plain_clock_time_is_last_resort() -> None:
    """An unhighlighted time is used when nothing else matches."""
    assert extract_delivery_datetime("Doručíme v 10:11.", RECEIVED_AT) == datetime(
        2026, 9, 20, 10, 11, tzinfo=PRAGUE
    )


def test_plain_past_clock_time_is_tomorrow() -> None:
    """An unhighlighted time gets no grace: once passed, it is tomorrow's."""
    assert extract_delivery_datetime(
        "Objednávka přijata v 9:40, doručíme co nejdříve", RECEIVED_AT
    ) == datetime(2026, 9, 21, 9, 40, tzinfo=PRAGUE)


def test_za_in_other_sense_is_not_a_countdown() -> None:
    """"za" followed by other words (an apology for a delay) is not a countdown."""
    assert extract_delivery_datetime(
        "Omlouváme se za zpoždění 15 minut, doručíme v 10:30.", RECEIVED_AT
    ) == datetime(2026, 9, 20, 10, 30, tzinfo=PRAGUE)


def test_overdue_countdown_is_due_now(freezer) -> None:
    """A countdown that has run out while unchanged resolves to now, not the past."""
    freezer.move_to(RECEIVED_AT + timedelta(minutes=10))

    assert extract_delivery_datetime(
        "Váš nákup doručíme přibližně za 2 minuty.", RECEIVED_AT
    ) == RECEIVED_AT + timedelta(minutes=10)


def test_date_after_new_year() -> None:
    """A date announced in late December for early January is next year's."""
    content = f"Doručíme {HIGHLIGHT.format('1.1.')} v {HIGHLIGHT.format('08:00')}"
    received_at = datetime(2026, 12, 31, 22, 0, tzinfo=PRAGUE)

    assert extract_delivery_datetime(content, received_at) == datetime(
        2027, 1, 1, 8, 0, tzinfo=PRAGUE
    )


def test_literal_unicode_escapes_are_decoded() -> None:
    """Literal \\uXXXX escapes are still decoded before matching."""
    content = "Doru\\u010d\\u00edme p\\u0159ibli\\u017en\\u011b za 4 minuty."

    assert extract_delivery_datetime(content, RECEIVED_AT) == RECEIVED_AT + timedelta(
        minutes=4
    )


@pytest.mark.parametrize(
    "content",
    [
        "Kurýr je u vás.",
        "Objednávku připravujeme.",
        "",
    ],
)
def test_no_eta(content: str) -> None:
    """Announcements without any time information yield None."""
    assert extract_delivery_datetime(content, RECEIVED_AT) is None



# Issue #106: the announced date and time sit in spans without a colour, next to
# the order-edit deadline "neděle 00:50". Received two days before delivery.
ISSUE_106_CONTENT = (
    "Váš nákup přivezeme <span>1.11.</span> v <span>09:00</span>. "
    "Až do <span>neděle 00:50</span> si k objednávce můžete cokoliv přidat!"
)
ISSUE_106_RECEIVED_AT = datetime(2026, 10, 30, 19, 16, tzinfo=PRAGUE)
ISSUE_106_SLOT = datetime(2026, 11, 1, 9, 0, tzinfo=PRAGUE)
ISSUE_106_SLOT_END = datetime(2026, 11, 1, 10, 0, tzinfo=PRAGUE)


@pytest.mark.parametrize("slot_since", [None, ISSUE_106_SLOT])
def test_unstyled_date_and_time(slot_since: datetime | None) -> None:
    """Spans without a colour still carry the date, with or without the slot."""
    assert extract_delivery_datetime(
        ISSUE_106_CONTENT, ISSUE_106_RECEIVED_AT, slot_since=slot_since
    ) == datetime(2026, 11, 1, 9, 0, tzinfo=PRAGUE)


def test_deadline_span_before_delivery_time() -> None:
    """A deadline span with a weekday is skipped even when it comes first."""
    content = (
        "Až do <span>neděle 00:50</span> můžete přidávat. "
        "Přivezeme <span>1.11.</span> v <span>09:00</span>."
    )

    assert extract_delivery_datetime(content, ISSUE_106_RECEIVED_AT) == datetime(
        2026, 11, 1, 9, 0, tzinfo=PRAGUE
    )


def test_unstyled_time_only() -> None:
    """A bare time in a plain span is read like a coloured one."""
    assert extract_delivery_datetime(
        "Doručíme v <span>10:04</span>", RECEIVED_AT
    ) == datetime(2026, 9, 20, 10, 4, tzinfo=PRAGUE)


def test_coloured_span_preferred_over_plain_span() -> None:
    """A plain-span time elsewhere in the text does not beat the coloured one."""
    content = f"Až do <span>20:00</span> lze přidávat. Doručíme v {HIGHLIGHT.format('10:30')}."

    assert extract_delivery_datetime(content, RECEIVED_AT) == datetime(
        2026, 9, 20, 10, 30, tzinfo=PRAGUE
    )


def test_countdown_beats_plain_span_time() -> None:
    """A countdown wins over a time in a plain span, as over unwrapped text."""
    content = "Doručíme za 5 minut (okno <span>10:00</span>–11:00)."

    assert extract_delivery_datetime(content, RECEIVED_AT) == RECEIVED_AT + timedelta(
        minutes=5
    )


@pytest.mark.parametrize(
    "content",
    [
        "Váš nákup přivezeme v 09:15.",
        "Váš nákup přivezeme v <span>09:15</span>.",
        f"Váš nákup přivezeme v {HIGHLIGHT.format('09:15')}.",
    ],
)
def test_clock_time_placed_on_slot_day(content: str) -> None:
    """With the slot known, a time without a date lands on the slot's day.

    Placing it after the receipt time instead would give tomorrow (31.10.).
    """
    assert extract_delivery_datetime(
        content,
        ISSUE_106_RECEIVED_AT,
        slot_since=ISSUE_106_SLOT,
        slot_till=ISSUE_106_SLOT_END,
    ) == datetime(2026, 11, 1, 9, 15, tzinfo=PRAGUE)


@pytest.mark.parametrize(
    ("slot_since", "clock", "expected_eta"),
    [
        # Early courier before a just-after-midnight slot: the evening before.
        (
            datetime(2026, 9, 21, 0, 30, tzinfo=PRAGUE),
            "23:50",
            datetime(2026, 9, 20, 23, 50, tzinfo=PRAGUE),
        ),
        # Late courier after a late-evening slot: just after midnight.
        (
            datetime(2026, 9, 20, 23, 30, tzinfo=PRAGUE),
            "00:20",
            datetime(2026, 9, 21, 0, 20, tzinfo=PRAGUE),
        ),
        # Very late courier, 12 h after the slot ends: that evening, although
        # the previous evening is only 11 h before the slot starts.
        (
            datetime(2026, 11, 1, 10, 0, tzinfo=PRAGUE),
            "23:00",
            datetime(2026, 11, 1, 23, 0, tzinfo=PRAGUE),
        ),
        # Courier a few hours early on the slot's day.
        (
            datetime(2026, 11, 1, 18, 0, tzinfo=PRAGUE),
            "13:00",
            datetime(2026, 11, 1, 13, 0, tzinfo=PRAGUE),
        ),
    ],
)
def test_clock_time_fits_slot(
    slot_since: datetime, clock: str, expected_eta: datetime
) -> None:
    """The day is the one that fits the one-hour slot best, lateness preferred."""
    content = f"Doručíme v {HIGHLIGHT.format(clock)}"

    assert extract_delivery_datetime(
        content,
        RECEIVED_AT,
        slot_since=slot_since,
        slot_till=slot_since + timedelta(hours=1),
    ) == expected_eta


def test_slot_without_end_uses_its_start() -> None:
    """A slot with no end is treated as a single moment."""
    assert extract_delivery_datetime(
        f"Doručíme v {HIGHLIGHT.format('09:15')}",
        ISSUE_106_RECEIVED_AT,
        slot_since=ISSUE_106_SLOT,
    ) == datetime(2026, 11, 1, 9, 15, tzinfo=PRAGUE)


def test_slot_day_beats_date_in_text() -> None:
    """With the slot known, a date that disagrees with it is not trusted.

    "11.1." is how a month-first language might write 1 November.
    """
    content = f"Doručíme {HIGHLIGHT.format('11.1.')} v {HIGHLIGHT.format('09:00')}"

    assert extract_delivery_datetime(
        content, ISSUE_106_RECEIVED_AT, slot_since=ISSUE_106_SLOT
    ) == datetime(2026, 11, 1, 9, 0, tzinfo=PRAGUE)


def test_slot_in_another_timezone_is_converted() -> None:
    """The slot's day is taken in the shop's timezone, not the slot's own offset.

    23:30 UTC on 31.10. is 00:30 on 1.11. in Prague.
    """
    slot = datetime(2026, 10, 31, 23, 30, tzinfo=ZoneInfo("UTC"))
    content = f"Doručíme v {HIGHLIGHT.format('00:45')}"

    assert extract_delivery_datetime(
        content, ISSUE_106_RECEIVED_AT, slot_since=slot
    ) == datetime(2026, 11, 1, 0, 45, tzinfo=PRAGUE)


def test_countdown_ignores_slot() -> None:
    """A relative countdown counts from receipt, wherever the slot is."""
    slot = RECEIVED_AT + timedelta(hours=5)

    assert extract_delivery_datetime(
        "Váš nákup doručíme přibližně za 2 minuty.", RECEIVED_AT, slot_since=slot
    ) == RECEIVED_AT + timedelta(minutes=2)
