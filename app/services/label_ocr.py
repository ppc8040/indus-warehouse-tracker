"""
Label OCR Service
=================
Parses a pallet label image (base64-encoded) to extract all 4 fields:
  - indus_part
  - customer_part
  - pallet_qty
  - handling_unit

Approach:
  1. Decode base64 → PIL Image → grayscale + contrast-enhance
  2. Run pytesseract OCR on the processed image
  3. Apply regex patterns to extracted text for each field

If pytesseract is not available (e.g. buildpack not configured),
falls back gracefully with a clear error rather than crashing.
"""
import re
import base64
import io
import logging

logger = logging.getLogger(__name__)

# Field regex patterns matched against the OCR-extracted text.
# Patterns are anchored to the label field names for accuracy.
_PATTERNS = {
    'indus_part':    r'Indus\s+Part[:\s]+([A-Z0-9]{3,20})',
    'customer_part': r'Customer\s+Part[:\s]+([A-Z0-9\-]{2,20})',
    'pallet_qty':    r'Pallet\s+Quantity[:\s]+(\d{1,6})',
    'handling_unit': r'Handling\s+Unit[:\s]+(\d{10,20})',
}

# Fallback: bare long-number on its own line (for when label text is absent but
# the HU number is printed clearly under the barcode)
_HU_BARE = r'(?:^|\s)(\d{15,20})(?:\s|$)'


def parse_label_image(b64_image: str) -> dict:
    """
    Parse a base64 image string and return extracted label fields.

    Args:
        b64_image: Data-URL or raw base64 string of the label photo.

    Returns:
        dict with keys: indus_part, customer_part, pallet_qty, handling_unit
        Missing/undetected fields are empty strings.

    Raises:
        ImportError: if Pillow or pytesseract is not installed.
        Exception:   on any image decode or OCR failure.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
    except ImportError:
        raise ImportError(
            "Pillow is required for photo scanning. "
            "Add 'Pillow' to requirements.txt and redeploy."
        )

    try:
        import pytesseract
    except ImportError:
        raise ImportError(
            "pytesseract is required for photo scanning. "
            "Add 'pytesseract' to requirements.txt, add 'Aptfile' with "
            "'tesseract-ocr', and add the heroku-community/apt buildpack."
        )

    # Decode base64 — handle both data-URL format and raw base64
    raw_b64 = b64_image.split(',')[-1]
    img_bytes = base64.b64decode(raw_b64)

    img = Image.open(io.BytesIO(img_bytes))

    # Pre-process: grayscale → sharpen → enhance contrast
    # These steps significantly improve OCR accuracy on printed labels
    img = img.convert('L')
    img = img.filter(ImageFilter.SHARPEN)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)

    # PSM 6: assume a single uniform block of text (good for label layout)
    text = pytesseract.image_to_string(img, config='--psm 6')
    logger.debug("OCR raw text: %r", text)

    result = {}
    for field, pattern in _PATTERNS.items():
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        result[field] = m.group(1).strip() if m else ''

    # Fallback for HU: bare long number if regex above missed it
    if not result.get('handling_unit'):
        m = re.search(_HU_BARE, text, re.MULTILINE)
        if m:
            result['handling_unit'] = m.group(1).strip()

    # Strip 'PC' suffix from pallet_qty if OCR picked it up
    if result.get('pallet_qty'):
        result['pallet_qty'] = re.sub(r'\s*(PC|PCS|pcs|pc).*', '', result['pallet_qty']).strip()

    return result
