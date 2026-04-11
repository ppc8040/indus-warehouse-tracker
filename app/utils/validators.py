"""
Input validation and sanitization.
"""
import re

# ── Handling Unit ──
# 18 to 20 digits, all numeric, no special characters.
# The barcode scanner may emit the value with a leading apostrophe
# (Excel text-prefix); that is stripped before validation.
HU_PATTERN = re.compile(r'^\d{18,20}$')
HU_MIN_LENGTH = 18
HU_MAX_LENGTH = 20

# ── Indus Part ──
# Exactly 7 characters: first 3 must be uppercase letters (A-Z),
# remaining 4 must be letters or digits (uppercase). No special characters.
# Example: CEJ1773, CCR2324
INDUS_PART_PATTERN = re.compile(r'^[A-Z]{3}[A-Z0-9]{4}$')
INDUS_PART_LENGTH = 7

# ── Customer Part ──
# No fixed format, but special characters are NOT allowed (except hyphen
# and dot which appear in real part codes like ENG-1440 or 161878.0).
# Must not be equal to Indus Part.
# Allowed chars: letters, digits, hyphen, dot, underscore, space.
CUSTOMER_PART_ALLOWED = re.compile(r'^[A-Za-z0-9\-._/ ]+$')

# ── Quantity ──
# Positive integer only, 1–4 digits. No special characters, no decimals.
QTY_PATTERN = re.compile(r'^\d{1,4}$')

VALID_MOVEMENTS = ('STOCK_IN', 'STOCK_OUT')
HTML_TAG_RE = re.compile(r'<[^>]+>')


def validate_handling_unit(hu_value):
    """Returns (is_valid, sanitized_or_error).

    Enforced rules:
      - 18 to 20 characters long
      - All digits (numeric only), no special characters
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
        return False, (
            "Invalid barcode. Handling Unit must contain digits only — "
            "no letters or special characters."
        )

    length = len(sanitized)
    if not (HU_MIN_LENGTH <= length <= HU_MAX_LENGTH):
        return False, (
            f"Invalid barcode length ({length} digits). "
            f"Handling Unit must be {HU_MIN_LENGTH}–{HU_MAX_LENGTH} digits long. "
            "Scan again or type carefully."
        )

    if not HU_PATTERN.match(sanitized):
        return False, "Invalid barcode format. Scan the correct label."

    return True, sanitized


def validate_indus_part(indus_part_value):
    """Returns (is_valid, sanitized_or_error).

    Enforced rules:
      - Exactly 7 characters
      - First 3 characters: uppercase letters only (A-Z)
      - Remaining 4 characters: uppercase letters or digits (A-Z, 0-9)
      - No special characters
    Example valid values: CEJ1773, CCR2324, ABX0099
    """
    if not indus_part_value:
        return False, "Indus Part is required."

    sanitized = indus_part_value.strip().upper()

    if not sanitized:
        return False, "Indus Part is empty after cleaning."

    if len(sanitized) != INDUS_PART_LENGTH:
        return False, (
            f"Invalid Indus Part length ({len(sanitized)} chars). "
            f"Indus Part must be exactly {INDUS_PART_LENGTH} characters (e.g. CEJ1773)."
        )

    if not sanitized[:3].isalpha():
        return False, (
            "Invalid Indus Part. The first 3 characters must be uppercase letters "
            f"(e.g. CEJ1773). Got '{sanitized[:3]}'."
        )

    if not INDUS_PART_PATTERN.match(sanitized):
        return False, (
            "Invalid Indus Part format. Must be 3 uppercase letters followed by "
            "4 letters/digits, with no special characters (e.g. CEJ1773)."
        )

    return True, sanitized


def validate_customer_part(customer_part_value, indus_part_value=""):
    """Returns (is_valid, sanitized_or_error).

    Enforced rules:
      - Allowed characters: letters, digits, hyphen, dot, underscore, slash, space
      - No other special characters
      - Must not be equal to Indus Part (case-insensitive)
    """
    if not customer_part_value:
        return True, ""  # Customer Part is optional

    sanitized = customer_part_value.strip()

    if not sanitized:
        return True, ""

    if not CUSTOMER_PART_ALLOWED.match(sanitized):
        return False, (
            "Invalid Customer Part. Only letters, digits, hyphens (-), dots (.), "
            "underscores (_), slashes (/), and spaces are allowed."
        )

    # Must not equal Indus Part
    if indus_part_value and sanitized.upper() == str(indus_part_value).strip().upper():
        return False, (
            "Customer Part must not be the same as Indus Part. "
            f"Both cannot be '{sanitized}'."
        )

    return True, sanitized


def validate_pallet_qty(qty_value):
    """Returns (is_valid, sanitized_or_error).

    Enforced rules:
      - Must be a positive integer (>= 1)
      - 1 to 4 digits only (max 9999)
      - No special characters, no decimals
    """
    if not qty_value:
        return True, ""  # Qty is optional (not always required to proceed)

    sanitized = str(qty_value).strip()

    # Handle Excel float leakage: '39.0' → '39'
    if sanitized.endswith('.0') and sanitized[:-2].isdigit():
        sanitized = sanitized[:-2]

    if not sanitized:
        return True, ""

    if not QTY_PATTERN.match(sanitized):
        return False, (
            "Invalid Quantity. Must be a whole number (1–9999) with no special characters, "
            "no decimals, and no letters (e.g. 39, 120, 1000)."
        )

    qty_int = int(sanitized)
    if qty_int < 1:
        return False, "Quantity must be a positive number (at least 1)."

    return True, sanitized


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
