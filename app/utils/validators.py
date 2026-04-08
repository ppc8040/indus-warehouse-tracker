"""
Input validation and sanitization.
"""
import re

# Handling Unit format: exactly 18 digits, must start with '1'
# Example: 100000000000829103
# The barcode scanner may emit the value with a leading apostrophe
# (Excel text-prefix); that is stripped before validation.
HU_PATTERN = re.compile(r'^1\d{17}$')
VALID_MOVEMENTS = ('STOCK_IN', 'STOCK_OUT')
HTML_TAG_RE = re.compile(r'<[^>]+>')

HU_LENGTH = 18


def validate_handling_unit(hu_value):
    """Returns (is_valid, sanitized_or_error).

    Enforced rules:
      - Exactly 18 characters
      - All digits (numeric only)
      - Must start with '1'
    """
    if not hu_value:
        return False, "No barcode value provided."

    sanitized = hu_value.strip().replace('\n', '').replace('\r', '').replace('\t', '')
    sanitized = sanitized.strip('\x00\x01\x02\x03')

    # Strip Excel text-prefix apostrophe if the scanner/sheet passed it through
    if sanitized.startswith("'"):
        sanitized = sanitized[1:]

    if not sanitized:
        return False, "Barcode is empty after cleaning."

    if not sanitized.isdigit():
        return False, "Invalid barcode. Handling Unit must contain digits only."

    if len(sanitized) != HU_LENGTH:
        return False, (
            f"Invalid barcode length ({len(sanitized)} digits). "
            f"Handling Unit must be exactly {HU_LENGTH} digits. Scan again."
        )

    if not sanitized.startswith('1'):
        return False, (
            f"Invalid barcode. Handling Unit must start with 1. "
            f"Got '{sanitized[0]}'. Scan the correct label."
        )

    if not HU_PATTERN.match(sanitized):
        return False, "Invalid barcode format. Scan the correct label."

    return True, sanitized  # already digits-only, no .upper() needed


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
