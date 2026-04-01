"""
Input validation and sanitization.
"""
import re

HU_PATTERN = re.compile(r'^[A-Za-z0-9]{4,30}$')
VALID_MOVEMENTS = ('STOCK_IN', 'STOCK_OUT')
HTML_TAG_RE = re.compile(r'<[^>]+>')


def validate_handling_unit(hu_value):
    """Returns (is_valid, sanitized_or_error)."""
    if not hu_value:
        return False, "No barcode value provided."
    sanitized = hu_value.strip().replace('\n', '').replace('\r', '').replace('\t', '')
    sanitized = sanitized.strip('\x00\x01\x02\x03')
    if not sanitized:
        return False, "Barcode is empty after cleaning."
    if len(sanitized) < 4:
        return False, "Barcode too short. Scan again."
    if len(sanitized) > 30:
        return False, "Barcode too long. Scan the correct label."
    if not HU_PATTERN.match(sanitized):
        return False, "Invalid barcode. Only letters and numbers allowed."
    return True, sanitized.upper()


def validate_movement_type(movement_type):
    """Returns (is_valid, sanitized_or_error)."""
    if not movement_type:
        return False, "Movement type is required."
    mt = movement_type.strip().upper()
    if mt not in VALID_MOVEMENTS:
        return False, "Invalid movement type. Must be STOCK_IN or STOCK_OUT."
    return True, mt


def validate_supervisor_pin(pin, correct_pin):
    if not pin or not correct_pin:
        return False
    return pin.strip() == correct_pin.strip()


def sanitize_text(text, max_len=500):
    if not text:
        return ""
    return HTML_TAG_RE.sub('', str(text)).strip()[:max_len]
