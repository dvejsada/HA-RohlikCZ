from datetime import timedelta, datetime, time
from zoneinfo import ZoneInfo
import re

def extract_delivery_datetime(text: str) -> datetime | None:
    """
    Extract delivery time information from various formatted strings and return a datetime object.

    Handles three types of delivery messages:
    1. Time only (HH:MM): "delivery at 17:23"
    2. Date and time: "delivery on 26.4. at 08:00"
    3. Minutes until delivery: "delivery in approximately 3 minutes"

    Args:
        text: HTML text containing delivery time information

    Returns:
        A timezone-aware datetime object representing the delivery time, or None if no valid time found
    """

    # Replace Unicode escape sequences
    clean_text: str = text.encode('utf-8').decode('unicode_escape')

    # Get plain text without HTML tags for pattern detection
    plain_text: str = re.sub(r'<[^>]+>', '', clean_text)

    prague_tz = ZoneInfo('Europe/Prague')
    now = datetime.now(tz=prague_tz)
    current_year: int = now.year

    # Check for Type 3: Minutes until delivery
    if re.search(r'(přibližně za|za)\s*.*\s*(minut|minuty|min)', plain_text, re.IGNORECASE):
        # Extract number of minutes from highlighted span
        minutes_pattern: re.Pattern = re.compile(r'<span[^>]*color:[^>]*>([0-9]+)</span>')

        matches = re.finditer(minutes_pattern, clean_text)
        minutes_matches: list[str] = [match.group(1) for match in matches]

        if minutes_matches:
            try:
                minutes: int = int(minutes_matches[0])
                # Calculate the estimated delivery time
                return now + timedelta(minutes=minutes)
            except ValueError:
                pass

    # Check for Type 2: Date and time
    date_pattern = re.compile(r'<span[^>]*color:[^>]*>([0-9]{1,2}\.[0-9]{1,2}\.)</span>')
    time_pattern = re.compile(r'<span[^>]*color:[^>]*>([0-9]{1,2}:[0-9]{2})</span>')

    matches_date = re.finditer(date_pattern, clean_text)
    date_matches = [match.group(1) for match in matches_date]

    matches_time = re.finditer(time_pattern, clean_text)
    time_matches = [match.group(1) for match in matches_time]

    if date_matches and time_matches:
        # We have both date and time
        try:
            date_str: str = date_matches[0]  # e.g., "26.4."
            day, month = map(int, date_str.replace('.', ' ').split())

            time_str: str = time_matches[0]  # e.g., "08:00"
            hour, minute = map(int, time_str.split(':'))

            # Create full delivery datetime
            delivery_dt = datetime(
                current_year, month, day, hour, minute,
                tzinfo=prague_tz
            )

            return delivery_dt
        except (ValueError, IndexError):
            pass

    # Check for Type 1: Time only
    if time_matches:
        try:
            time_str: str = time_matches[0]  # e.g., "17:23"
            hour, minute = map(int, time_str.split(':'))

            # Use today's date with the specified time
            today = now.date()

            # If the time has already passed today, it might refer to tomorrow
            delivery_dt = datetime.combine(today, time(hour, minute))
            delivery_dt = delivery_dt.replace(tzinfo=prague_tz)

            if delivery_dt < now:
                # Time already passed today, assume it's for tomorrow
                tomorrow = today + timedelta(days=1)
                delivery_dt = datetime.combine(tomorrow, time(hour, minute))
                delivery_dt = delivery_dt.replace(tzinfo=prague_tz)

            return delivery_dt
        except (ValueError, IndexError):
            pass

    # If no structured time information was found, try to extract any time mention
    # Generic time pattern search in the plain text
    plain_time_matches = re.findall(r'\b([0-9]{1,2}:[0-9]{2})\b', plain_text)
    if plain_time_matches:
        try:
            time_str: str = plain_time_matches[0]
            hour, minute = map(int, time_str.split(':'))

            # Use today's date with the specified time
            today = now.date()

            delivery_dt = datetime.combine(today, time(hour, minute))
            delivery_dt = delivery_dt.replace(tzinfo=prague_tz)

            # If the time has already passed today, it might refer to tomorrow
            if delivery_dt < now:
                tomorrow = today + timedelta(days=1)
                delivery_dt = datetime.combine(tomorrow, time(hour, minute))
                delivery_dt = delivery_dt.replace(tzinfo=prague_tz)

            return delivery_dt
        except (ValueError, IndexError):
            pass

    # No valid time information found
    return None