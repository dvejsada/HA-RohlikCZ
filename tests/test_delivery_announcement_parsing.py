"""Tests for extracting the delivery ETA from an announcement."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.rohlikcz.utils import extract_delivery_datetime

PRAGUE = ZoneInfo("Europe/Prague")


def _minutes_ahead(content: str) -> int | None:
    """Return the parsed ETA as whole minutes from now."""
    parsed = extract_delivery_datetime(content)
    if parsed is None:
        return None
    return round((parsed - datetime.now(PRAGUE)).total_seconds() / 60)


def test_short_variant_without_clock_time() -> None:
    """The final-approach variant carries the ETA only as plain text."""
    assert _minutes_ahead("Váš nákup doručíme přibližně za 9 minut.") == 9


def test_short_variant_declension() -> None:
    """Czech declension of 'minuta' is recognised as well."""
    assert _minutes_ahead("Váš nákup doručíme přibližně za 2 minuty.") == 2


def test_highlighted_clock_time_still_wins() -> None:
    """A highlighted clock time keeps precedence over the minute count."""
    clock = (datetime.now(PRAGUE) + timedelta(minutes=30)).replace(
        second=0, microsecond=0
    )
    content = (
        "Váš nákup doručíme přibližně za 29 minut, tedy v "
        f'<span style="color:#009B37">{clock:%H:%M}</span>.'
    )

    assert extract_delivery_datetime(content) == clock


def test_highlighted_minute_count_is_used() -> None:
    """A highlighted minute count keeps being honoured."""
    content = (
        'Doručíme přibližně za <span style="color:#009B37">7</span> minut.'
    )

    assert _minutes_ahead(content) == 7


def test_announcement_without_any_time_information() -> None:
    """An announcement carrying no ETA still yields no value."""
    assert extract_delivery_datetime("Kurýr je u vás.") is None
