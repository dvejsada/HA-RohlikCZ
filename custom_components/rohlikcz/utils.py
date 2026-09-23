import codecs
from datetime import timedelta, datetime, time
import html
import json
from zoneinfo import ZoneInfo
import re


def calculate_current_month_orders_total(orders: list) -> float|None:
    """
    Calculate the total amount of orders for the current month from JSON data.

    Args:
        orders (list): List of order dictionaries, each containing an 'orderTime' and 'priceComposition'

    Returns:
        float: Total amount of orders for the current month. Returns 0.0 if no orders
               are found for the current month or None if the JSON is invalid.

    """
    try:

        # Get current month pattern (e.g., "2025-06-")
        current_month_pattern = datetime.now().strftime("%Y-%m-")

        # Filter orders from current month and calculate sum
        total_amount = 0.0

        for order in orders:
            try:
                # Simple string check for current month
                if current_month_pattern in order['orderTime']:
                    amount = float(order['priceComposition']['total']['amount'])
                    total_amount += amount
            except (KeyError, ValueError, TypeError):
                # Skip invalid orders
                continue

        return total_amount

    except (json.JSONDecodeError, TypeError):
        return None


# "za 2 minuty" / "za 10 minut" / "za 1 minutu" / "za cca 5 minut" /
# "za 2-3 minuty" / "za 2 až 3 minuty" (Czech plural forms, up to two words
# before the number, ranges resolve to their lower bound), or the numberless
# "za minutu". Anchored on "za" so a clock time such as "10:11" can never be read
# as a minute count.
_MINUTES_UNTIL_PATTERN = re.compile(
    r'\bza\s+(?:[^\W\d_]+\.?\s+){0,2}?'
    r'(?:(\d{1,3})(?:\s*(?:[-–]|až)\s*\d{1,3})?\s*(?:minut|min\b)|minutu\b)',
    re.IGNORECASE,
)

# Highlighted date ("26.4.") and clock time ("08:00"), and any clock time.
_HIGHLIGHTED_DATE_PATTERN = re.compile(r'<span[^>]*color:[^>]*>([0-9]{1,2}\.[0-9]{1,2}\.)</span>')
_HIGHLIGHTED_TIME_PATTERN = re.compile(r'<span[^>]*color:[^>]*>([0-9]{1,2}:[0-9]{2})</span>')
_PLAIN_TIME_PATTERN = re.compile(r'\b([0-9]{1,2}:[0-9]{2})\b')

# Literal backslash escapes (\u010d, \xa0, \n, ...) in a double-encoded payload.
_ESCAPE_PATTERN = re.compile(r'\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[nrt"\'\\])')

# A clock time up to this far in the past still refers to today (a late courier,
# or an announcement re-read after a restart); anything earlier is tomorrow's.
_PASSED_TIME_GRACE = timedelta(hours=1)


def _resolve_clock_time(hour: int, minute: int, now: datetime) -> datetime:
    """Place a bare HH:MM on today, or on tomorrow if it has clearly passed."""
    delivery_dt = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo)
    if delivery_dt < now - _PASSED_TIME_GRACE:
        tomorrow = now.date() + timedelta(days=1)
        delivery_dt = datetime.combine(tomorrow, time(hour, minute), tzinfo=now.tzinfo)
    return delivery_dt


def extract_delivery_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """
    Extract delivery time information from various formatted strings and return a datetime object.

    Handles three types of delivery messages, in this order of preference:
    1. Highlighted date and time: "delivery on 26.4. at 08:00"
    2. Highlighted time only (HH:MM): "delivery at 17:23"
    3. Minutes until delivery: "delivery in approximately 3 minutes"
    Any other HH:MM mentioned in the text is used as a last resort.

    Args:
        text: HTML text containing delivery time information
        now: Reference time the announcement was received at. Relative
            ("in 3 minutes") messages are counted from it, and clock times are
            placed on its day. Defaults to the current time.

    Returns:
        A timezone-aware datetime object representing the delivery time, or None if no valid time found
    """

    # Decode literal backslash escapes. The text normally arrives already
    # decoded from JSON, so non-ASCII characters (Czech diacritics) must be left
    # untouched rather than round-tripped through the unicode_escape codec.
    clean_text: str = _ESCAPE_PATTERN.sub(
        lambda m: codecs.decode(m.group(0), 'unicode_escape'), text
    )

    # Get plain text without HTML tags or entities (&nbsp;) for pattern detection
    plain_text: str = html.unescape(re.sub(r'<[^>]+>', '', clean_text))

    prague_tz = ZoneInfo('Europe/Prague')
    now = now.astimezone(prague_tz) if now is not None else datetime.now(tz=prague_tz)

    # Type 1: Date and time
    date_matches = _HIGHLIGHTED_DATE_PATTERN.findall(clean_text)
    time_matches = _HIGHLIGHTED_TIME_PATTERN.findall(clean_text)

    if date_matches and time_matches:
        try:
            day, month = map(int, date_matches[0].replace('.', ' ').split())  # e.g. "26.4."
            hour, minute = map(int, time_matches[0].split(':'))  # e.g. "08:00"
            delivery_dt = datetime(now.year, month, day, hour, minute, tzinfo=prague_tz)
            if delivery_dt < now - timedelta(days=180):
                # Announced in December for early January.
                delivery_dt = delivery_dt.replace(year=now.year + 1)
            return delivery_dt
        except (ValueError, IndexError):
            pass

    # Type 2: Highlighted time only
    if time_matches:
        try:
            hour, minute = map(int, time_matches[0].split(':'))  # e.g. "17:23"
            return _resolve_clock_time(hour, minute, now)
        except ValueError:
            pass

    # Type 3: Minutes until delivery, e.g. the short "Váš nákup doručíme
    # přibližně za 2 minuty." sent during the final approach without a clock time
    minutes_match = _MINUTES_UNTIL_PATTERN.search(plain_text)
    if minutes_match:
        minutes = int(minutes_match.group(1)) if minutes_match.group(1) else 1
        return now + timedelta(minutes=minutes)

    # Last resort: any time mention in the plain text
    plain_time_matches = _PLAIN_TIME_PATTERN.findall(plain_text)
    if plain_time_matches:
        try:
            hour, minute = map(int, plain_time_matches[0].split(':'))
            return _resolve_clock_time(hour, minute, now)
        except ValueError:
            pass

    # No valid time information found
    return None


def parse_delivery_datetime_string(datetime_str: str) -> datetime | None:
    """
    Parse a delivery datetime string with fallback for different formats.
    
    Args:
        datetime_str (str): Datetime string to parse
        
    Returns:
        datetime: Parsed datetime object, or None if parsing fails
    """
    if datetime_str is None:
        return None
    
    try:
        # Try parsing with microseconds first (format: 2025-12-18T08:15:01.000+0100)
        return datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        # Try without microseconds if the format doesn't match
        try:
            return datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            # Try with timezone with colon separator (format: 2025-12-18T08:15:01.000+01:00)
            try:
                # Replace +0100 with +01:00 for parsing
                if datetime_str[-5] in ['+', '-'] and ':' not in datetime_str[-5:]:
                    datetime_str_colon = datetime_str[:-2] + ':' + datetime_str[-2:]
                    return datetime.strptime(datetime_str_colon, "%Y-%m-%dT%H:%M:%S.%f%z")
            except (ValueError, IndexError):
                # If normalization or parsing still fails (or the string is too short),
                # fall through and let the function return None to signal parse failure.
                pass
            return None


def get_earliest_order(orders: list) -> dict | None:
    """
    Find the order with the earliest delivery time from a list of orders.

    Args:
        orders (list): List of order dictionaries, each containing a 'deliverySlot' with 'since' field

    Returns:
        dict: The order with the earliest delivery time, or None if no valid order found
    """
    if not orders:
        return None

    earliest_order = None
    earliest_time = None

    for order in orders:
        try:
            # Extract delivery slot and since time
            delivery_slot = order.get("deliverySlot", {})
            since_str = delivery_slot.get("since", None)

            if since_str is None:
                continue

            # Parse the datetime string (format: "%Y-%m-%dT%H:%M:%S.%f%z")
            try:
                delivery_time = datetime.strptime(since_str, "%Y-%m-%dT%H:%M:%S.%f%z")
            except ValueError:
                # Try without microseconds if the format doesn't match
                try:
                    delivery_time = datetime.strptime(since_str, "%Y-%m-%dT%H:%M:%S%z")
                except ValueError:
                    # Skip orders with invalid date format
                    continue

            # Check if this is the earliest order so far
            if earliest_time is None or delivery_time < earliest_time:
                earliest_time = delivery_time
                earliest_order = order

        except (KeyError, TypeError, AttributeError):
            # Skip orders with missing or invalid structure
            continue

    return earliest_order


def parse_orders_for_calendar(next_orders: list[dict], delivered_orders: list[dict]) -> list[dict]:
    """
    Parse and combine orders from next_order and delivered_orders into a normalized list for calendar events.
    
    Args:
        next_orders: List of upcoming orders from next_order endpoint
        delivered_orders: List of delivered orders (last 50) from delivered_orders endpoint
        
    Returns:
        List of normalized order dictionaries with:
        - id: Order ID (string)
        - start: Delivery slot start datetime (timezone-aware)
        - end: Delivery slot end datetime (timezone-aware)
        - status: Order status if available
        - items_count: Number of items if available
        - price: Order price if available
    """
    import logging
    _logger = logging.getLogger(__name__)
    
    normalized_orders = []
    seen_order_ids = set()
    skipped_no_slot = 0
    skipped_no_datetime = 0
    skipped_invalid = 0
    
    # Process next_orders first (prefer these as they're more current)
    for order in next_orders or []:
        try:
            order_id = order.get('id')
            if not order_id:
                continue
            
            order_id_str = str(order_id)
            if order_id_str in seen_order_ids:
                continue
            
            delivery_slot = order.get('deliverySlot')
            if not delivery_slot or not isinstance(delivery_slot, dict):
                skipped_no_slot += 1
                continue
            
            since_str = delivery_slot.get('since')
            till_str = delivery_slot.get('till')
            
            if not since_str or not till_str:
                skipped_no_slot += 1
                continue
            
            start_dt = parse_delivery_datetime_string(since_str)
            end_dt = parse_delivery_datetime_string(till_str)
            
            if not start_dt or not end_dt:
                skipped_no_datetime += 1
                _logger.warning("Order %s: Failed to parse datetime - since: %s (parsed: %s), till: %s (parsed: %s)", 
                              order_id_str, since_str, start_dt, till_str, end_dt)
                continue
            
            # Ensure start is before end
            if start_dt >= end_dt:
                skipped_invalid += 1
                _logger.warning("Order %s: Invalid time range - start (%s) >= end (%s)", order_id_str, start_dt, end_dt)
                continue
            
            normalized_order = {
                'id': order_id_str,
                'start': start_dt,
                'end': end_dt,
                'status': order.get('status'),
                'items_count': order.get('itemsCount'),
                'price': order.get('priceComposition', {}).get('total', {}).get('amount') if order.get('priceComposition') else None
            }
            
            normalized_orders.append(normalized_order)
            seen_order_ids.add(order_id_str)
            
        except (KeyError, TypeError, ValueError) as e:
            skipped_invalid += 1
            _logger.debug("Error processing next_order %s: %s", order.get('id'), e)
            continue
    
    # Process delivered_orders (skip if already seen in next_orders)
    for order in delivered_orders or []:
        try:
            order_id = order.get('id')
            if not order_id:
                continue
            
            order_id_str = str(order_id)
            if order_id_str in seen_order_ids:
                continue
            
            delivery_slot = order.get('deliverySlot')
            if not delivery_slot or not isinstance(delivery_slot, dict):
                skipped_no_slot += 1
                # Delivered orders typically don't have delivery slot information
                # Skip them as we need start/end times to create calendar events
                continue
            
            since_str = delivery_slot.get('since')
            till_str = delivery_slot.get('till')
            
            # Skip if no delivery slot (delivered orders might not have it)
            if not since_str or not till_str:
                skipped_no_slot += 1
                _logger.debug("Order %s: Missing since/till in delivery slot. Slot keys: %s", order_id_str, list(delivery_slot.keys()))
                continue
            
            start_dt = parse_delivery_datetime_string(since_str)
            end_dt = parse_delivery_datetime_string(till_str)
            
            if not start_dt or not end_dt:
                skipped_no_datetime += 1
                _logger.warning("Order %s: Failed to parse datetime - since: %s (parsed: %s), till: %s (parsed: %s)", 
                              order_id_str, since_str, start_dt, till_str, end_dt)
                continue
            
            # Ensure start is before end
            if start_dt >= end_dt:
                skipped_invalid += 1
                _logger.warning("Order %s: Invalid time range - start (%s) >= end (%s)", order_id_str, start_dt, end_dt)
                continue
            
            normalized_order = {
                'id': order_id_str,
                'start': start_dt,
                'end': end_dt,
                'status': order.get('status'),
                'items_count': order.get('itemsCount'),
                'price': order.get('priceComposition', {}).get('total', {}).get('amount') if order.get('priceComposition') else None
            }
            
            normalized_orders.append(normalized_order)
            seen_order_ids.add(order_id_str)
            
        except (KeyError, TypeError, ValueError) as e:
            skipped_invalid += 1
            _logger.debug("Error processing delivered_order %s: %s", order.get('id'), e)
            continue
    
    _logger.info(
        "parse_orders_for_calendar: Processed %d next_orders + %d delivered_orders, "
        "created %d normalized orders, skipped: %d no slot, %d no datetime, %d invalid",
        len(next_orders) if next_orders else 0,
        len(delivered_orders) if delivered_orders else 0,
        len(normalized_orders),
        skipped_no_slot,
        skipped_no_datetime,
        skipped_invalid
    )
    
    # Sort by start datetime ascending
    normalized_orders.sort(key=lambda x: x['start'])
    
    return normalized_orders