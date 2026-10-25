"""
forensics.py - EXIF IMAGE FORENSICS MODULE for VeriBot AI.

This module inspects uploaded marksheets, degree certificates, and ID card images
for metadata anomalies, digital image editing software signatures, and structural tampering.

WHAT THIS MODULE DOES:
    1. Reads EXIF metadata using Pillow (PIL).
    2. Searches EXIF software tags and metadata for signatures of photo/graphics editors
       (e.g. Adobe Photoshop, Canva, GIMP, Adobe Illustrator, PicsArt, CorelDRAW).
    3. Checks image structural properties (dimensions, color mode, compression hints).
    4. Automatically generates a high-severity "FORGERY DETECTED" flag when digital editing software signatures are found.
"""

import io
import hashlib
import json
import logging
from PIL import Image, ImageOps

logger = logging.getLogger("veribot.forensics")
OCR_CACHE_TTL_SECONDS = 24 * 60 * 60
OCR_MAX_DIMENSION = 2400
OCR_MAX_PIXELS = 40_000_000
OCR_TIMEOUT_SECONDS = 10

# Software keywords known to be associated with image manipulation/editing programs
SOFTWARE_EDITORS = [
    "photoshop", "canva", "gimp", "illustrator", "picsart",
    "coreldraw", "paint.net", "pixlr", "affinity photo", "lightroom",
    "snapseed", "fotor", "befunky", "photopea", "adobe"
]


def _ocr_cache():
    try:
        from queue_worker import redis_client

        return redis_client
    except ImportError:
        return None


def ocr_image_bytes(image_bytes: bytes, tesseract_cmd: str = "") -> tuple[str, str]:
    """OCR an image with bounded dimensions/time and a content-addressed Redis cache."""
    digest = hashlib.sha256(image_bytes).hexdigest()
    cache_key = "veribot_ocr:" + digest
    cache = _ocr_cache()
    if cache is not None:
        try:
            cached = cache.get(cache_key)
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8")
            if cached:
                result = json.loads(cached)
                return result["text"], result["note"]
        except Exception:
            logger.warning("Could not read OCR cache for image %s", digest)

    try:
        import pytesseract
    except ImportError:
        return "", "OCR libraries are not installed (pip install pillow pytesseract)."

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            if opened.width * opened.height > OCR_MAX_PIXELS:
                raise ValueError("Image resolution exceeds the OCR safety limit.")
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((OCR_MAX_DIMENSION, OCR_MAX_DIMENSION), Image.Resampling.LANCZOS)
            image = image.convert("L")
            text = pytesseract.image_to_string(image, timeout=OCR_TIMEOUT_SECONDS).strip()
        result = {"text": text, "note": ""}
    except Exception as error:
        result = {
            "text": "",
            "note": "OCR could not run or exceeded its 10-second limit. (" + str(error)[:120] + ")",
        }

    if cache is not None and not result["note"]:
        try:
            cache.set(cache_key, json.dumps(result), ex=OCR_CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("Could not write OCR cache for image %s", digest)
    return result["text"], result["note"]


def analyze_image_forensics(filename: str, image_bytes: bytes) -> dict:
    """
    Inspect an image file's bytes for EXIF metadata, software signatures, and potential editing indicators.

    Parameters:
        filename (str): Name of the uploaded file.
        image_bytes (bytes): Raw binary content of the image.

    Returns:
        dict: A report containing software signatures, detected editing tags, and generated forgery flags.
    """
    forensic_report = {
        "filename": filename,
        "is_image": True,
        "software_detected": None,
        "edited": False,
        "flags": [],
        "exif_summary": {}
    }

    try:
        image = Image.open(io.BytesIO(image_bytes))
        forensic_report["exif_summary"]["format"] = image.format
        forensic_report["exif_summary"]["size"] = image.size
        forensic_report["exif_summary"]["mode"] = image.mode

        # Extract EXIF data if present
        exif_data = image._getexif() if hasattr(image, "_getexif") else None

        software_signature = ""

        if exif_data:
            # Tag 305 is the EXIF Software tag in standard EXIF tags
            # Tag 271 is Make, Tag 272 is Model
            for tag_id, value in exif_data.items():
                # Tag 305 = 'Software'
                if tag_id == 305 and isinstance(value, str):
                    software_signature += " " + value
                elif tag_id in (271, 272) and isinstance(value, str):
                    software_signature += " " + value

        # Also inspect general info dictionary attached by Pillow
        if hasattr(image, "info") and isinstance(image.info, dict):
            for k, v in image.info.items():
                if isinstance(v, str):
                    software_signature += " " + v

        software_signature_lower = software_signature.lower()

        # Check if any known editing software keyword is present in the extracted software/info metadata
        detected_editors = []
        for editor in SOFTWARE_EDITORS:
            if editor in software_signature_lower:
                detected_editors.append(editor)

        if detected_editors:
            editor_names = ", ".join([ed.title() for ed in set(detected_editors)])
            forensic_report["software_detected"] = editor_names
            forensic_report["edited"] = True

            # Generate high-severity forgery flag
            forensic_report["flags"].append({
                "severity": "high",
                "text": f"FORGERY DETECTED: Modified in Image Editor ({editor_names}) in document '{filename}'.",
                "source": "forensics",
                "kind": "trust"
            })

    except Exception as error:
        forensic_report["is_image"] = False
        forensic_report["exif_summary"]["error"] = f"Could not analyze image metadata: {str(error)[:100]}"

    return forensic_report
