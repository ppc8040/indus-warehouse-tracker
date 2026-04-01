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

Heroku note:
  Tesseract is installed via heroku-buildpack-apt to /app/.apt/usr/bin/tesseract.
  The binary path is set explicitly below so pytesseract finds it.
  TESSDATA_PREFIX config var must also be set on Heroku.
"""
import re
import base64
import io
import os
import logging

logger = logging.getLogger(__name__)

# Heroku installs apt packages under /app/.apt/
# Set this so pytesseract can find the binary at runtime.
# Heroku installs tesseract via heroku-buildpack-apt.
# Try both the v4 and v5 paths — Heroku-22 uses 4.x, Heroku-24 uses 5.x
_TESSERACT_PATHS = [
    '/app/.apt/usr/bin/tesseract',       # primary Heroku apt path
    '/usr/bin/tesseract',                 # fallback: system install (local dev)
    '/usr/local/bin/tesseract',           # fallback: homebrew / manual install
]

_TESSDATA_PATHS = [
    '/app/.apt/usr/share/tesseract-ocr/5/tessdata',     # Heroku-24 (Ubuntu 24, tesseract 5)
    '/app/.apt/usr/share/tesseract-ocr/4.00/tessdata',  # Heroku-22 (Ubuntu 22, tesseract 4)
    '/usr/share/tesseract-ocr/5/tessdata',               # system
    '/usr/share/tesseract-ocr/4.00/tessdata',            # system fallback
]

# Field regex patterns matched against the OCR-extracted text.
_PATTERNS = {
    'indus_part':    r'Indus\s+Part[:\s]+([A-Z0-9]{3,20})',
    'customer_part': r'Customer\s+Part[:\s]+([A-Z0-9\-]{2,20})',
    'pallet_qty':    r'Pallet\s+Quantity[:\s]+(\d{1,6})',
    'handling_unit': r'Handling\s+Unit[:\s]+(\d{10,20})',
}

# Fallback: bare long-number on its own line (HU number printed under barcode)
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
            "'tesseract-ocr' and 'tesseract-ocr-eng', and add the "
            "heroku-community/apt buildpack."
        )

    # Point pytesseract at the Heroku-installed binary if it exists there.
    # Falls back to system default (e.g. local dev) if not found.
    # Auto-detect tesseract binary and tessdata path
    for tpath in _TESSERACT_PATHS:
        if os.path.isfile(tpath):
            pytesseract.pytesseract.tesseract_cmd = tpath
            logger.info("Using tesseract at: %s", tpath)
            break
    else:
        raise RuntimeError(
            "Tesseract binary not found. Checked: " + ", ".join(_TESSERACT_PATHS) +
            ". Add heroku-community/apt buildpack and Aptfile with tesseract-ocr, then redeploy."
        )

    for dpath in _TESSDATA_PATHS:
        if os.path.isdir(dpath):
            os.environ['TESSDATA_PREFIX'] = dpath
            logger.info("Using tessdata at: %s", dpath)
            break

    # Decode base64 — handle both data-URL format and raw base64
    raw_b64 = b64_image.split(',')[-1]
    img_bytes = base64.b64decode(raw_b64)

    img = Image.open(io.BytesIO(img_bytes))

    # Pre-process: grayscale → sharpen → enhance contrast
    # Significantly improves OCR accuracy on printed labels
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

    # Fallback for HU: bare long number if field-label regex missed it
    if not result.get('handling_unit'):
        m = re.search(_HU_BARE, text, re.MULTILINE)
        if m:
            result['handling_unit'] = m.group(1).strip()

    # Strip 'PC'/'PCS' suffix from pallet_qty if OCR picked it up
    if result.get('pallet_qty'):
        result['pallet_qty'] = re.sub(r'\s*(PC|PCS|pcs|pc).*', '', result['pallet_qty']).strip()

    return result
