from datetime import timedelta, datetime, time
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


# "za 2 minuty" / "za 10 minut" / "za 1 minutu" (Czech plural forms), or the
# numberless "za minutu". Anchored on "za" so a clock time such as "10:11" can
# never be read as a minute count.
_MINUTES_UNTIL_PATTERN = re.compile(
    r'\bza\s+(?:(\d{1,3})\s*(?:minut|min\b)|minutu\b)', re.IGNORECASE
)


def extract_delivery_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """
    Extract delivery time information from various formatted strings and return a datetime object.

    Handles three types of delivery messages:
    1. Time only (HH:MM): "delivery at 17:23"
    2. Date and time: "delivery on 26.4. at 08:00"
    3. Minutes until delivery: "delivery in approximately 3 minutes"

    Args:
        text: HTML text containing delivery time information
        now: Reference time the announcement was received at. Relative
            ("in 3 minutes") messages are counted from it, and clock times
            earlier than it roll over to the next day. Defaults to the current
            time.

    Returns:
        A timezone-aware datetime object representing the delivery time, or None if no valid time found
    """

    # Decode literal \uXXXX escape sequences. The text normally arrives already
    # decoded from JSON, so non-ASCII characters (Czech diacritics) must be left
    # untouched rather than round-tripped through the unicode_escape codec.
    clean_text: str = re.sub(
        r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text
    )

    # Get plain text without HTML tags for pattern detection
    plain_text: str = re.sub(r'<[^>]+>', '', clean_text)

    prague_tz = ZoneInfo('Europe/Prague')
    now = now.astimezone(prague_tz) if now is not None else datetime.now(tz=prague_tz)
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

    # Last resort: a plain minute count without any clock time, e.g. the short
    # "Váš nákup doručíme přibližně za 2 minuty." sent during the final approach.
    minutes_match = _MINUTES_UNTIL_PATTERN.search(plain_text)
    if minutes_match:
        minutes = int(minutes_match.group(1)) if minutes_match.group(1) else 1
        return now + timedelta(minutes=minutes)

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