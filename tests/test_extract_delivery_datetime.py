"""Tests for parsing the delivery ETA out of Rohlík's delivery announcement."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.rohlikcz.utils import extract_delivery_datetime

PRAGUE = ZoneInfo("Europe/Prague")
RECEIVED_AT = datetime(2026, 9, 20, 10, 3, 36, tzinfo=PRAGUE)
HIGHLIGHT = '<span style="color:#009B37">{}</span>'


@pytest.mark.parametrize(
    ("content", "minutes"),
    [
        ("Váš nákup doručíme přibližně za 2 minuty.", 2),
        ("Váš nákup doručíme přibližně za 9 minut.", 9),
        ("Váš nákup doručíme přibližně za 1 minutu.", 1),
        ("Váš nákup doručíme přibližně za minutu.", 1),
        (f"Váš nákup doručíme přibližně za {HIGHLIGHT.format('2 minuty')}.", 2),
        (f"Váš nákup doručíme přibližně za {HIGHLIGHT.format(3)} minuty.", 3),
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
        "Váš nákup doručíme přibližně za 10 minut, tedy v 10:11.",
        f"Váš nákup doručíme přibližně za 10 minut, tedy v {HIGHLIGHT.format('10:11')}.",
    ],
)
def test_clock_time_wins_over_minute_count(content: str) -> None:
    """The long announcement keeps using its exact clock time."""
    assert extract_delivery_datetime(content, RECEIVED_AT) == datetime(
        2026, 9, 20, 10, 11, tzinfo=PRAGUE
    )


def test_clock_time_is_relative_to_reference_time() -> None:
    """A clock time is resolved against when the announcement was received.

    Once the ETA has passed, re-parsing the same announcement must not roll it
    over to the next day.
    """
    content = f"Doručíme v {HIGHLIGHT.format('10:04')}"
    received_at = datetime(2026, 9, 20, 10, 0, tzinfo=PRAGUE)

    assert extract_delivery_datetime(content, received_at) == datetime(
        2026, 9, 20, 10, 4, tzinfo=PRAGUE
    )


def test_date_and_time() -> None:
    """A date plus time announcement is unaffected."""
    content = f"Doručíme {HIGHLIGHT.format('21.9.')} v {HIGHLIGHT.format('08:00')}"

    assert extract_delivery_datetime(content, RECEIVED_AT) == datetime(
        2026, 9, 21, 8, 0, tzinfo=PRAGUE
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
